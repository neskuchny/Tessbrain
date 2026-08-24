"""W30: Messenger account linking — onboarding tokens + lookup.

Поток:
1. User в UI жмёт "Connect Telegram" → `POST /messengers/telegram/onboarding/start`
   → backend issues OnboardingToken(token, expires_at=10min, user_id, platform)
2. Frontend генерит deep-link `https://t.me/<bot>?start=<token>` и
   открывает в новой вкладке.
3. User кликает Start в боте → бот отправляет `/start <token>` —
   webhook прилетает, мы видим `text="/start <token>"` + `external_id`.
4. `redeem_token(token, external_id, external_username)` создаёт
   MessengerLink, помечает token used.
5. Все последующие сообщения от этого external_id роутятся под user_id.

Best-effort: при отсутствии postgres методы возвращают None / пустые
списки и логируют — caller (webhook handler) корректно отвечает "link
unavailable".
"""
from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


SUPPORTED_PLATFORMS = frozenset({"telegram", "slack", "whatsapp"})
DEFAULT_TOKEN_TTL_MINUTES = 10


def _new_link_id() -> str:
    return f"mlk_{uuid.uuid4().hex[:16]}"


def _new_token() -> str:
    # 32 hex chars, URL-safe (≈128 bit entropy).
    return secrets.token_hex(16)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MessengerLink:
    id: str
    user_id: str
    platform: str
    external_id: str
    external_username: Optional[str]
    active: bool
    linked_at: str
    last_seen_at: Optional[str]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MessengerLink":
        def _iso(v: Any) -> Optional[str]:
            if v is None:
                return None
            return v.isoformat() if hasattr(v, "isoformat") else str(v)
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            platform=row["platform"],
            external_id=row["external_id"],
            external_username=row.get("external_username"),
            active=bool(row.get("active", True)),
            linked_at=_iso(row.get("linked_at")) or _utc_now().isoformat(),
            last_seen_at=_iso(row.get("last_seen_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "platform": self.platform,
            "external_id": self.external_id,
            "external_username": self.external_username,
            "active": self.active,
            "linked_at": self.linked_at,
            "last_seen_at": self.last_seen_at,
        }


@dataclass
class OnboardingToken:
    token: str
    user_id: str
    platform: str
    expires_at: str
    used_at: Optional[str] = None

    def is_valid(self, *, now: Optional[datetime] = None) -> bool:
        if self.used_at is not None:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except Exception:
            return False
        n = now or _utc_now()
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > n


_INSERT_TOKEN_SQL = text("""
INSERT INTO public.messenger_onboarding_tokens
    (token, user_id, platform, expires_at)
VALUES (:token, :user_id, :platform, :expires_at)
""")

_GET_TOKEN_SQL = text("""
SELECT token, user_id, platform, expires_at, used_at
FROM public.messenger_onboarding_tokens
WHERE token = :token
""")

_USE_TOKEN_SQL = text("""
UPDATE public.messenger_onboarding_tokens
SET used_at = now(), used_by_external_id = :external_id
WHERE token = :token AND used_at IS NULL
""")

_INSERT_LINK_SQL = text("""
INSERT INTO public.messenger_links
    (id, user_id, platform, external_id, external_username, linked_at)
VALUES (:id, :user_id, :platform, :external_id, :external_username, now())
ON CONFLICT (platform, external_id) DO UPDATE
    SET user_id = EXCLUDED.user_id,
        external_username = EXCLUDED.external_username,
        active = TRUE,
        linked_at = now()
""")

_LOOKUP_LINK_SQL = text("""
SELECT id, user_id, platform, external_id, external_username,
       active, linked_at, last_seen_at
FROM public.messenger_links
WHERE platform = :platform AND external_id = :external_id AND active = TRUE
""")

_LIST_USER_LINKS_SQL = text("""
SELECT id, user_id, platform, external_id, external_username,
       active, linked_at, last_seen_at
FROM public.messenger_links
WHERE user_id = :user_id AND active = TRUE
ORDER BY linked_at DESC
""")

_DEACTIVATE_LINK_SQL = text("""
UPDATE public.messenger_links
SET active = FALSE
WHERE id = :id
""")

_TOUCH_LINK_SQL = text("""
UPDATE public.messenger_links
SET last_seen_at = now()
WHERE id = :id
""")


class MessengerLinkService:
    """CRUD над messenger_links + onboarding tokens.

    Без postgres → in-memory fallback (для dev/тестов).
    """

    def __init__(self, *, postgres: Any = None) -> None:
        self.postgres = postgres
        self._mem_links: dict[tuple[str, str], MessengerLink] = {}
        self._mem_tokens: dict[str, OnboardingToken] = {}
        self._mem_user_links: dict[str, list[str]] = {}

    # === Onboarding tokens ============================================

    async def issue_token(
        self,
        *,
        user_id: str,
        platform: str,
        ttl_minutes: int = DEFAULT_TOKEN_TTL_MINUTES,
    ) -> OnboardingToken:
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"unsupported platform: {platform}")
        token = _new_token()
        expires = _utc_now() + timedelta(minutes=max(1, int(ttl_minutes)))
        rec = OnboardingToken(
            token=token,
            user_id=user_id,
            platform=platform,
            expires_at=expires.isoformat(),
        )
        if self.postgres is None:
            self._mem_tokens[token] = rec
            return rec
        try:
            async with self.postgres.session() as session:
                await session.execute(_INSERT_TOKEN_SQL, {
                    "token": token,
                    "user_id": user_id,
                    "platform": platform,
                    "expires_at": expires,
                })
        except Exception as exc:
            logger.warning("issue_token failed: %s — falling back to memory", exc)
            self._mem_tokens[token] = rec
        return rec

    async def redeem_token(
        self,
        *,
        token: str,
        external_id: str,
        external_username: Optional[str] = None,
    ) -> Optional[MessengerLink]:
        """Обменять token на MessengerLink. None если token невалиден."""
        if not token or not external_id:
            return None
        rec = await self._get_token(token)
        if rec is None or not rec.is_valid():
            return None
        link = MessengerLink(
            id=_new_link_id(),
            user_id=rec.user_id,
            platform=rec.platform,
            external_id=external_id,
            external_username=external_username,
            active=True,
            linked_at=_utc_now().isoformat(),
            last_seen_at=None,
        )
        if self.postgres is None:
            key = (link.platform, link.external_id)
            existing = self._mem_links.get(key)
            if existing is not None:
                link.id = existing.id
            self._mem_links[key] = link
            self._mem_user_links.setdefault(link.user_id, [])
            if link.id not in self._mem_user_links[link.user_id]:
                self._mem_user_links[link.user_id].append(link.id)
            self._mem_tokens[token] = OnboardingToken(
                token=token, user_id=rec.user_id, platform=rec.platform,
                expires_at=rec.expires_at, used_at=_utc_now().isoformat(),
            )
            return link
        try:
            async with self.postgres.session() as session:
                await session.execute(_INSERT_LINK_SQL, {
                    "id": link.id,
                    "user_id": link.user_id,
                    "platform": link.platform,
                    "external_id": link.external_id,
                    "external_username": link.external_username,
                })
                await session.execute(_USE_TOKEN_SQL, {
                    "token": token, "external_id": external_id,
                })
        except Exception as exc:
            logger.warning("redeem_token failed: %s", exc)
            return None
        return link

    async def _get_token(self, token: str) -> Optional[OnboardingToken]:
        if self.postgres is None:
            return self._mem_tokens.get(token)
        try:
            async with self.postgres.session() as session:
                result = await session.execute(_GET_TOKEN_SQL, {"token": token})
                row = await _first_row(result)
        except Exception as exc:
            logger.warning("_get_token failed: %s", exc)
            return None
        if row is None:
            return None

        def _iso(v: Any) -> Optional[str]:
            if v is None:
                return None
            return v.isoformat() if hasattr(v, "isoformat") else str(v)

        return OnboardingToken(
            token=row["token"],
            user_id=row["user_id"],
            platform=row["platform"],
            expires_at=_iso(row.get("expires_at")) or _utc_now().isoformat(),
            used_at=_iso(row.get("used_at")),
        )

    # === Links lookup =================================================

    async def lookup_by_external(
        self, *, platform: str, external_id: str,
    ) -> Optional[MessengerLink]:
        """Найти активный link по (platform, external_id) — для inbound routing."""
        if not platform or not external_id:
            return None
        if self.postgres is None:
            return self._mem_links.get((platform, external_id))
        try:
            async with self.postgres.session() as session:
                result = await session.execute(_LOOKUP_LINK_SQL, {
                    "platform": platform, "external_id": external_id,
                })
                row = await _first_row(result)
        except Exception as exc:
            logger.warning("lookup_by_external failed: %s", exc)
            return None
        return MessengerLink.from_row(row) if row else None

    async def find_link_for_user(
        self, *, user_id: str, platform: str,
    ) -> Optional[MessengerLink]:
        """Первый АКТИВНЫЙ link юзера на платформе (telegram/slack/…).

        Используется исходящей доставкой (кофе-дайджест, автоматизации,
        процессные доски): «куда слать этому пользователю в Telegram» —
        это chat_id (external_id) его привязки к платформенному боту.
        """
        try:
            links = await self.list_for_user(user_id=user_id)
        except Exception as exc:
            logger.warning("find_link_for_user failed: %s", exc)
            return None
        for link in links:
            if link.platform == platform and link.active:
                return link
        return None

    async def list_for_user(self, *, user_id: str) -> list[MessengerLink]:
        if not user_id:
            return []
        if self.postgres is None:
            ids = self._mem_user_links.get(user_id, [])
            out = []
            for link in self._mem_links.values():
                if link.id in ids and link.active:
                    out.append(link)
            return out
        try:
            async with self.postgres.session() as session:
                result = await session.execute(_LIST_USER_LINKS_SQL, {
                    "user_id": user_id,
                })
                rows = await _all_rows(result)
        except Exception as exc:
            logger.warning("list_for_user failed: %s", exc)
            return []
        return [MessengerLink.from_row(r) for r in rows]

    async def deactivate(self, *, link_id: str) -> bool:
        if not link_id:
            return False
        if self.postgres is None:
            for key, link in list(self._mem_links.items()):
                if link.id == link_id:
                    link.active = False
                    return True
            return False
        try:
            async with self.postgres.session() as session:
                await session.execute(_DEACTIVATE_LINK_SQL, {"id": link_id})
            return True
        except Exception as exc:
            logger.warning("deactivate failed: %s", exc)
            return False

    async def touch(self, *, link_id: str) -> None:
        """Обновить last_seen_at — на каждом inbound message."""
        if not link_id:
            return
        if self.postgres is None:
            for link in self._mem_links.values():
                if link.id == link_id:
                    link.last_seen_at = _utc_now().isoformat()
                    return
            return
        try:
            async with self.postgres.session() as session:
                await session.execute(_TOUCH_LINK_SQL, {"id": link_id})
        except Exception as exc:
            logger.debug("touch failed: %s", exc)


# === Result-row helpers (mimic asyncpg/sqlalchemy mappings) =============


async def _first_row(result: Any) -> Optional[dict[str, Any]]:
    try:
        mappings = result.mappings()
        if hasattr(mappings, "first"):
            row = mappings.first()
            # ВАЖНО: .first() закрывает result. Пустой результат — это
            # ОТВЕТ (строки нет), а не повод для фолбэка: повторное чтение
            # закрытого result падало «This result object is closed», и
            # «привязки нет» превращалось в ошибку в логе.
            return dict(row) if row is not None else None
    except Exception:
        pass
    if hasattr(result, "first"):
        row = result.first()
        if row is None:
            return None
        if hasattr(row, "_mapping"):
            return dict(row._mapping)
        if isinstance(row, dict):
            return row
    return None


async def _all_rows(result: Any) -> list[dict[str, Any]]:
    try:
        mappings = result.mappings()
        rows = mappings.all() if hasattr(mappings, "all") else []
        return [dict(r) for r in rows]
    except Exception:
        pass
    if hasattr(result, "all"):
        rows = result.all()
        out = []
        for r in rows:
            if hasattr(r, "_mapping"):
                out.append(dict(r._mapping))
            elif isinstance(r, dict):
                out.append(r)
        return out
    return []


# === Модульный доступ для исходящей доставки ==========================
# coffee/delivery.py (и автоматизации) импортируют get_messenger_links_service —
# раньше функции НЕ СУЩЕСТВОВАЛО: импорт молча падал в try/except, и доставка
# «в привязанный Telegram» не работала никогда. Держим один экземпляр на
# процесс: сервис без состояния (кроме in-memory fallback без Postgres).

_service_instance: Optional[MessengerLinkService] = None


async def get_messenger_links_service() -> MessengerLinkService:
    """Singleton MessengerLinkService c postgres, если тот доступен."""
    global _service_instance
    if _service_instance is not None:
        return _service_instance
    postgres = None
    try:
        from backend.db.postgres import get_postgres
        postgres = await get_postgres()
    except Exception:
        postgres = None
    _service_instance = MessengerLinkService(postgres=postgres)
    return _service_instance


async def resolve_telegram_chat_id(user_id: str) -> Optional[str]:
    """chat_id для отправки пользователю в Telegram.

    Порядок: (1) привязка платформенного бота (messenger_links, external_id);
    (2) фолбэк — вручную заданный «Default Chat ID» из вкладки «Интеграции».

    Зачем фолбэк: привязка через бота требует, чтобы Telegram доставил боту
    `/start <token>` на ВЕБХУК. На локалхосте (нет публичного URL) вебхук
    недостижим — привязка не создаётся, и раньше доставка молчала, хотя юзер
    уже вписал Chat ID руками. Теперь при отсутствии привязки шлём на этот ID.
    Никогда не raises — best-effort, None если ничего не нашли."""
    if not user_id:
        return None
    # 1. Привязка платформенного бота.
    try:
        svc = await get_messenger_links_service()
        link = await svc.find_link_for_user(user_id=user_id, platform="telegram")
        cid = getattr(link, "external_id", None) if link else None
        if cid:
            return str(cid)
    except Exception:
        logger.debug("resolve_telegram_chat_id: link lookup failed", exc_info=True)
    # 2. Фолбэк на ручной Default Chat ID из «Интеграций».
    try:
        from backend.api.routes.integrations import get_user_integration_keys
        keys = await get_user_integration_keys(user_id, "telegram") or {}
        cid = str(keys.get("default_chat_id") or "").strip()
        if cid:
            return cid
    except Exception:
        logger.debug("resolve_telegram_chat_id: integration fallback failed", exc_info=True)
    return None


async def resolve_telegram_bot_token(user_id: str = "") -> Optional[str]:
    """Токен бота для отправки: платформенный (env TELEGRAM_BOT_TOKEN /
    settings.telegram_bot_token) → BYO bot_token пользователя из «Интеграций».

    Даёт включить доставку, ВПИСАВ токен во вкладке «Интеграции», без правки
    окружения — как и остальные ключи. None, если токена нет нигде."""
    try:
        from backend.config import get_settings
        tok = (getattr(get_settings(), "telegram_bot_token", "") or "").strip()
    except Exception:
        tok = ""
    if not tok:
        import os
        tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if tok:
        return tok
    if user_id:
        try:
            from backend.api.routes.integrations import get_user_integration_keys
            keys = await get_user_integration_keys(user_id, "telegram") or {}
            byo = str(keys.get("bot_token") or "").strip()
            if byo:
                return byo
        except Exception:
            logger.debug("resolve_telegram_bot_token: integration fallback failed", exc_info=True)
    return None
