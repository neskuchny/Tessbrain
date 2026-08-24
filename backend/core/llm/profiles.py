"""W42: per-tenant LLM provider profiles.

Позволяет switch'ить активный LLM endpoint в runtime через UI:
- managed API: gemini / openai
- self-hosted: local_vllm (vLLM/Ollama/TGI)
- cloud GPU: runpod (proxy.runpod.net или api.runpod.ai)
- наш fork: turboquant (квантизованная модель на нашем GPU)

Архитектурно RUNPOD и TURBOQUANT — это варианты OpenAI-compatible client'а
с другим base_url. Различие в operational характеристиках (latency, cost,
security boundary) и labelling для Prometheus / audit.

Дизайн:
- API key в БД зашифрован через pgp_sym_encrypt (W11/050_pgcrypto)
- RLS по tenant_id (W4)
- Один is_default per tenant — атомарно clear через CLEAR_DEFAULT_SQL
- Best-effort: при отсутствии postgres работает только in-memory cache
- Health-check (test_profile) дёргает GET {base_url}/models — не отправляет
  реальный prompt, экономит токены
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

try:
    from backend.db.postgres import run_with_retry as _run_with_retry
except Exception:  # pragma: no cover — fallback, если модуль недоступен
    async def _run_with_retry(fn, **_):  # type: ignore[no-redef]
        return await fn()


# Phase 1 (multi-provider): добавили cloud-провайдеров через OpenAI-compat
# endpoints — для них достаточно base_url + api_key, ProfileBackedClient
# использует AsyncOpenAI как один универсальный backend. Anthropic ходит
# через свой нативный SDK (нет полной OpenAI-совместимости).
SUPPORTED_PROVIDERS = frozenset({
    "gemini", "openai", "local_vllm", "runpod", "turboquant",
    # Cloud API через OpenAI-compat:
    "deepseek",      # https://api.deepseek.com/v1
    "qwen",          # https://dashscope.aliyuncs.com/compatible-mode/v1 (DashScope)
    "openrouter",    # https://openrouter.ai/api/v1 (router across 100+ models)
    "together",      # https://api.together.xyz/v1
    "mistral",       # https://api.mistral.ai/v1
    "groq",          # https://api.groq.com/openai/v1
    "xai",           # https://api.x.ai/v1 (Grok, OpenAI-compat)
    "yandex",        # https://llm.api.cloud.yandex.net/v1 (OpenAI-compat;
                     # модель в формате URI: gpt://<folder_id>/yandexgpt/latest)
    # Native SDK:
    "anthropic",     # Claude — собственный SDK
    # Local через Ollama OpenAI-compat:
    "ollama",        # http://host.docker.internal:11434/v1
    # Phase 3 CLI-bridge: subprocess через локальный CLI с subscription auth.
    # Юзер делает `claude login` / `codex login` один раз — токены списываются
    # с подписки Claude Pro/Max / ChatGPT Plus, не через API-биллинг.
    # Требует установки CLI в Docker-образ + монтирования auth-credentials.
    "claude_cli",
    "codex_cli",
})

# Провайдеры использующие OpenAI-compatible API (base_url required).
# Anthropic исключён — у него собственный SDK с другим форматом.
# Cloud-API провайдеры (deepseek/qwen/openrouter/together/mistral/groq)
# имеют дефолтный base_url, но допускают override (для прокси/proxy_pass).
OPENAI_COMPAT_PROVIDERS = frozenset({
    "openai", "local_vllm", "runpod", "turboquant",
    "deepseek", "qwen", "openrouter", "together", "mistral", "groq", "ollama",
    "xai", "yandex",
})

# Провайдеры с дефолтным cloud-endpoint'ом — base_url можно НЕ задавать,
# подставится ProfileBackedClient'ом.
DEFAULT_BASE_URLS: dict[str, str] = {
    "openai":     "https://api.openai.com/v1",
    "deepseek":   "https://api.deepseek.com/v1",
    "qwen":       "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "together":   "https://api.together.xyz/v1",
    "mistral":    "https://api.mistral.ai/v1",
    "groq":       "https://api.groq.com/openai/v1",
    "xai":        "https://api.x.ai/v1",
    "yandex":     "https://llm.api.cloud.yandex.net/v1",
    "ollama":     "http://host.docker.internal:11434/v1",
}

# Провайдеры, которые ходят к чужой инфраструктуре. Список — общий с
# проверкой периметра (backend.core.security.perimeter), чтобы «внешний»
# значило одно и то же и здесь, и в роутере: раньше расхождение позволяло
# завести в закрытом контуре профиль на публичное облако.
# Локальные (local_vllm, ollama, turboquant) сюда не входят.
from backend.core.security.perimeter import (  # noqa: E402
    EXTERNAL_LLM_PROVIDERS as EXTERNAL_PROVIDERS,
)


# Sentinel для partial-update: «поле не передано» ≠ «поле = None».
# None — валидное значение (напр. очистить base_url), поэтому нужен отдельный
# маркер отсутствия, а не проверка на None.
_UNSET: Any = object()


def _new_id() -> str:
    return f"llm_{uuid.uuid4().hex[:16]}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


@dataclass
class LLMProfile:
    """Read projection — то что отдаём API + используем при routing."""
    id: str
    name: str
    provider: str
    model: str
    base_url: Optional[str]
    is_default: bool
    is_fallback: bool
    config: dict[str, Any]
    last_health_check_at: Optional[str]
    last_health_ok: Optional[bool]
    created_at: str
    updated_at: str
    has_api_key: bool   # не отдаём сам ключ, только bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "LLMProfile":
        cfg = row.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except Exception:
                cfg = {}
        return cls(
            id=row["id"],
            name=row["name"],
            provider=row["provider"],
            model=row["model"],
            base_url=row.get("base_url"),
            is_default=bool(row.get("is_default", False)),
            is_fallback=bool(row.get("is_fallback", False)),
            config=cfg if isinstance(cfg, dict) else {},
            last_health_check_at=_iso(row.get("last_health_check_at")),
            last_health_ok=row.get("last_health_ok"),
            created_at=_iso(row.get("created_at")) or _utc_now().isoformat(),
            updated_at=_iso(row.get("updated_at")) or _utc_now().isoformat(),
            has_api_key=bool(row.get("api_key_encrypted")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "is_default": self.is_default,
            "is_fallback": self.is_fallback,
            "config": self.config,
            "has_api_key": self.has_api_key,
            "last_health_check_at": self.last_health_check_at,
            "last_health_ok": self.last_health_ok,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ProfileNotFound(Exception):
    pass


class ProfileConflict(Exception):
    pass


class ProfileValidationError(ValueError):
    pass


def validate_profile_input(
    *,
    name: str,
    provider: str,
    model: str,
    base_url: Optional[str],
    api_key: Optional[str],
) -> None:
    if not name or not name.strip():
        raise ProfileValidationError("name required")
    if len(name) > 100:
        raise ProfileValidationError("name too long (max 100 chars)")
    if provider not in SUPPORTED_PROVIDERS:
        raise ProfileValidationError(
            f"unsupported provider: {provider} (allowed: {sorted(SUPPORTED_PROVIDERS)})"
        )
    if not model or not model.strip():
        raise ProfileValidationError("model required")
    # base_url обязателен только для провайдеров без дефолтного endpoint'а
    # (local_vllm / turboquant — пользователь сам поднимает сервер; runpod —
    # его проксированный URL). Для cloud-провайдеров с дефолтом — опционально,
    # ProfileBackedClient подставит из DEFAULT_BASE_URLS.
    _base_url_required = OPENAI_COMPAT_PROVIDERS - set(DEFAULT_BASE_URLS.keys())
    if provider in _base_url_required:
        if not base_url or not base_url.strip():
            raise ProfileValidationError(
                f"base_url required for provider={provider} (OpenAI-compatible endpoint)"
            )
    if provider == "openai" and base_url and not base_url.startswith("https://api.openai.com"):
        # openai с custom base_url (Azure / proxy) — допустимо но warn.
        logger.info(
            "Profile uses provider=openai with custom base_url=%s — must be OpenAI-compatible",
            base_url,
        )
    # api_key обязателен для всех external cloud провайдеров. Local-only
    # (local_vllm/ollama/turboquant) — опционально.
    _key_required = EXTERNAL_PROVIDERS | {"openai", "gemini"}
    if provider in _key_required and not api_key:
        raise ProfileValidationError(
            f"api_key required for provider={provider}"
        )

    # Закрытый контур: профиль на внешнее облако не заводится вовсе.
    # Роутер такой профиль всё равно отклонит — но отказ на входе объясняет
    # администратору причину сразу, а не оставляет молча неработающий
    # профиль, помеченный как активный.
    from backend.core.security.perimeter import (
        enterprise_mode_enabled,
        llm_target_leaves_perimeter,
    )
    if enterprise_mode_enabled():
        reason = llm_target_leaves_perimeter(
            provider, base_url or DEFAULT_BASE_URLS.get(provider),
        )
        if reason:
            raise ProfileValidationError(
                f"закрытый контур (enterprise_mode): {reason}. "
                "Допустимы только локальные модели внутри вашей сети "
                "(local_vllm / ollama / turboquant)"
            )


_INSERT_SQL = text("""
INSERT INTO public.llm_profiles
    (id, name, provider, base_url, model, api_key_encrypted, is_default,
     is_fallback, config, created_by)
VALUES (:id, :name, :provider, :base_url, :model,
        CASE WHEN :api_key IS NULL OR :api_key = '' THEN NULL
             ELSE pgp_sym_encrypt(:api_key, :pgc_key, 'cipher-algo=aes256') END,
        :is_default, :is_fallback, CAST(:config AS JSONB), :created_by)
""")
_GET_BY_ID_SQL = text("""
SELECT id, name, provider, base_url, model,
       (api_key_encrypted IS NOT NULL) AS has_api_key,
       api_key_encrypted,
       is_default, is_fallback, config,
       last_health_check_at, last_health_ok,
       created_at, updated_at
FROM public.llm_profiles
WHERE id = :id
""")
_DECRYPT_SQL = text("""
SELECT pgp_sym_decrypt(api_key_encrypted, :pgc_key)::TEXT AS api_key
FROM public.llm_profiles
WHERE id = :id AND api_key_encrypted IS NOT NULL
""")
_LIST_SQL = text("""
SELECT id, name, provider, base_url, model,
       (api_key_encrypted IS NOT NULL) AS has_api_key,
       NULL::BYTEA AS api_key_encrypted,
       is_default, is_fallback, config,
       last_health_check_at, last_health_ok,
       created_at, updated_at
FROM public.llm_profiles
ORDER BY is_default DESC, created_at DESC
LIMIT :lim OFFSET :off
""")
_GET_DEFAULT_SQL = text("""
SELECT id, name, provider, base_url, model,
       (api_key_encrypted IS NOT NULL) AS has_api_key,
       api_key_encrypted,
       is_default, is_fallback, config,
       last_health_check_at, last_health_ok,
       created_at, updated_at
FROM public.llm_profiles
WHERE is_default = TRUE
LIMIT 1
""")
_CLEAR_DEFAULT_SQL = text("""
UPDATE public.llm_profiles
SET is_default = FALSE
WHERE is_default = TRUE AND id <> :id
""")
_SET_DEFAULT_SQL = text("""
UPDATE public.llm_profiles SET is_default = TRUE WHERE id = :id
""")
_TOUCH_HEALTH_SQL = text("""
UPDATE public.llm_profiles
SET last_health_check_at = now(), last_health_ok = :ok
WHERE id = :id
""")
_DELETE_SQL = text("""
DELETE FROM public.llm_profiles WHERE id = :id
""")
class LLMProfileService:
    """CRUD над llm_profiles + health-check + lookup активного.

    Без postgres → in-memory fallback (для тестов / dev режима).
    """

    def __init__(self, *, postgres: Any = None, pgc_key: Optional[str] = None) -> None:
        self.postgres = postgres
        # pgcrypto master key — берём из core.security.encryption если не передан.
        if pgc_key is None:
            try:
                from backend.core.security.encryption import get_field_key
                pgc_key = get_field_key()
            except Exception:
                pgc_key = ""
        self.pgc_key = pgc_key
        self._mem: dict[str, dict[str, Any]] = {}
        self._mem_keys: dict[str, str] = {}
        self._mem_default_id: Optional[str] = None
        # Negative-cache на «public.llm_profiles does not exist».
        # Если миграция 130_llm_profiles ещё не применена, на каждый LLM-вызов
        # шёл SELECT → ERROR в pg-логах. Кешируем (epoch_ts) на 60s — раз в
        # минуту пробуем снова (чтобы после apply миграций service ожил без
        # рестарта). Сбрасывается в create()/setattr.
        self._missing_table_until: float = 0.0

    # === Create ============================================================

    async def create(
        self,
        *,
        name: str,
        provider: str,
        model: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        is_default: bool = False,
        is_fallback: bool = False,
        config: Optional[dict[str, Any]] = None,
        created_by: Optional[str] = None,
    ) -> LLMProfile:
        validate_profile_input(
            name=name, provider=provider, model=model,
            base_url=base_url, api_key=api_key,
        )
        config = config or {}
        profile_id = _new_id()

        if self.postgres is None:
            row = {
                "id": profile_id,
                "name": name,
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "api_key_encrypted": b"x" if api_key else None,
                "is_default": is_default,
                "is_fallback": is_fallback,
                "config": config,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
            }
            # Атомарно clear других default'ов.
            if is_default:
                for r in self._mem.values():
                    r["is_default"] = False
                self._mem_default_id = profile_id
            self._mem[profile_id] = row
            if api_key:
                self._mem_keys[profile_id] = api_key
            return LLMProfile.from_row(row)

        try:
            async with self.postgres.session() as session:
                if is_default:
                    await session.execute(
                        _CLEAR_DEFAULT_SQL, {"id": profile_id},
                    )
                await session.execute(_INSERT_SQL, {
                    "id": profile_id,
                    "name": name,
                    "provider": provider,
                    "base_url": base_url,
                    "model": model,
                    "api_key": api_key or "",
                    "pgc_key": self.pgc_key,
                    "is_default": is_default,
                    "is_fallback": is_fallback,
                    "config": json.dumps(config, ensure_ascii=False),
                    "created_by": created_by,
                })
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ProfileConflict(f"profile name {name!r} already exists") from exc
            raise

        result = await self.get(profile_id=profile_id)
        if result is None:
            raise RuntimeError("profile create succeeded but reload failed")
        return result

    # === Read ==============================================================

    async def get(self, *, profile_id: str) -> Optional[LLMProfile]:
        if not profile_id:
            return None
        if self.postgres is None:
            row = self._mem.get(profile_id)
            return LLMProfile.from_row(row) if row else None

        async def _query():
            async with self.postgres.session() as session:
                result = await session.execute(_GET_BY_ID_SQL, {"id": profile_id})
                return await _first_row(result)

        try:
            row = await _run_with_retry(_query)
        except Exception as exc:
            logger.warning("LLMProfileService.get failed: %s", exc)
            return None
        return LLMProfile.from_row(row) if row else None

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[LLMProfile]:
        if self.postgres is None:
            rows = sorted(
                self._mem.values(),
                key=lambda r: (not r.get("is_default", False), str(r.get("created_at", ""))),
                reverse=False,
            )
            return [LLMProfile.from_row(r) for r in rows[offset:offset + limit]]
        async def _query():
            async with self.postgres.session() as session:
                result = await session.execute(_LIST_SQL, {
                    "lim": max(1, min(int(limit), 200)),
                    "off": max(0, int(offset)),
                })
                return await _all_rows(result)

        try:
            rows = await _run_with_retry(_query)
        except Exception as exc:
            logger.warning("LLMProfileService.list failed: %s", exc)
            return []
        return [LLMProfile.from_row(r) for r in rows]

    async def get_default(self) -> Optional[LLMProfile]:
        if self.postgres is None:
            if self._mem_default_id is None:
                return None
            row = self._mem.get(self._mem_default_id)
            return LLMProfile.from_row(row) if row else None
        # Negative-cache: если таблица отсутствует — не долбим БД на каждый
        # LLM-вызов (см. self._missing_table_until). Раз в 60s пробуем снова.
        import time
        if self._missing_table_until > time.time():
            return None

        async def _query():
            async with self.postgres.session() as session:
                result = await session.execute(_GET_DEFAULT_SQL, {})
                return await _first_row(result)

        try:
            # Ретрай на транзиентный обрыв коннекта (WinError 64 за Docker/WSL2
            # NAT) — свежая сессия на повторе обычно проходит; до negative-cache
            # доходим только при реально лежащей БД.
            row = await _run_with_retry(_query)
        except Exception as exc:
            msg = str(exc).lower()
            if "does not exist" in msg or "undefinedtable" in msg:
                self._missing_table_until = time.time() + 60
                logger.warning(
                    "LLMProfileService: таблица llm_profiles не найдена — "
                    "применить миграцию 130_llm_profiles.sql. "
                    "Negative-cache на 60s.")
            elif any(k in msg for k in (
                "winerror", "connection", "unreachable", "timeout",
                "сетевое имя", "could not connect", "refused",
                "is closed", "network", "operationalerror",
            )):
                # БД недоступна (напр. Postgres упал) — БЕЗ negative-cache
                # каждый LLM-вызов ждал ~30s на таймаут соединения, агент
                # становился неюзабельным. Кешируем на 30s → fallback на
                # env-модель мгновенно.
                self._missing_table_until = time.time() + 30
                logger.warning(
                    "LLMProfileService: БД недоступна (%s) — negative-cache "
                    "30s, используется env-модель.", exc)
            else:
                logger.warning("LLMProfileService.get_default failed: %s", exc)
            return None
        return LLMProfile.from_row(row) if row else None

    async def get_decrypted_api_key(self, *, profile_id: str) -> Optional[str]:
        """Returns plaintext key — only for backend use, never serialize в response."""
        if not profile_id:
            return None
        if self.postgres is None:
            return self._mem_keys.get(profile_id)
        if not self.pgc_key:
            logger.warning("get_decrypted_api_key: pgc_key not configured")
            return None
        try:
            async with self.postgres.session() as session:
                result = await session.execute(_DECRYPT_SQL, {
                    "id": profile_id, "pgc_key": self.pgc_key,
                })
                row = await _first_row(result)
        except Exception as exc:
            logger.warning("get_decrypted_api_key failed: %s", exc)
            return None
        if not row:
            return None
        return row.get("api_key") or None

    # === Update / Delete ==================================================

    async def set_default(self, *, profile_id: str) -> bool:
        if not profile_id:
            return False
        if self.postgres is None:
            if profile_id not in self._mem:
                raise ProfileNotFound(profile_id)
            for rid, r in self._mem.items():
                r["is_default"] = (rid == profile_id)
            self._mem_default_id = profile_id
            return True
        existing = await self.get(profile_id=profile_id)
        if existing is None:
            raise ProfileNotFound(profile_id)
        try:
            async with self.postgres.session() as session:
                await session.execute(
                    _CLEAR_DEFAULT_SQL, {"id": profile_id},
                )
                await session.execute(_SET_DEFAULT_SQL, {"id": profile_id})
            return True
        except Exception as exc:
            logger.warning("set_default failed: %s", exc)
            return False

    async def update(
        self,
        *,
        profile_id: str,
        name: Any = _UNSET,
        provider: Any = _UNSET,
        model: Any = _UNSET,
        base_url: Any = _UNSET,
        api_key: Any = _UNSET,
        is_default: Any = _UNSET,
        is_fallback: Any = _UNSET,
        config: Any = _UNSET,
    ) -> LLMProfile:
        """Partial-update профиля. Передавай только меняемые поля (`_UNSET` →
        не трогать). Пустая строка api_key очищает ключ; отсутствие поля —
        оставляет как есть (важно для маскированного поля в UI).

        Валидируем ИТОГОВЫЙ профиль (merge существующего + патча), чтобы,
        напр., смена provider на key-required не прошла без ключа.
        """
        existing = await self.get(profile_id=profile_id)
        if existing is None:
            raise ProfileNotFound(profile_id)

        final_name = existing.name if name is _UNSET else str(name or "").strip()
        final_provider = existing.provider if provider is _UNSET else str(provider or "").strip()
        final_model = existing.model if model is _UNSET else str(model or "").strip()
        final_base_url = existing.base_url if base_url is _UNSET else (base_url or None)
        # Будет ли ключ после апдейта: новый переданный ИЛИ уже существующий.
        # "x" — sentinel «ключ есть» для валидатора (сам ключ ему не нужен).
        if api_key is _UNSET:
            key_for_validation = "x" if existing.has_api_key else None
        else:
            key_for_validation = (api_key or None)
        validate_profile_input(
            name=final_name, provider=final_provider, model=final_model,
            base_url=final_base_url, api_key=key_for_validation,
        )

        # --- in-memory fallback ---
        if self.postgres is None:
            row = self._mem.get(profile_id)
            if row is None:
                raise ProfileNotFound(profile_id)
            if name is not _UNSET:
                row["name"] = final_name
            if provider is not _UNSET:
                row["provider"] = final_provider
            if model is not _UNSET:
                row["model"] = final_model
            if base_url is not _UNSET:
                row["base_url"] = final_base_url
            if is_fallback is not _UNSET:
                row["is_fallback"] = bool(is_fallback)
            if config is not _UNSET:
                row["config"] = config or {}
            if api_key is not _UNSET:
                if api_key:
                    self._mem_keys[profile_id] = api_key
                    row["api_key_encrypted"] = b"x"
                else:
                    self._mem_keys.pop(profile_id, None)
                    row["api_key_encrypted"] = None
            if is_default is not _UNSET:
                if is_default:
                    for r in self._mem.values():
                        r["is_default"] = False
                    row["is_default"] = True
                    self._mem_default_id = profile_id
                else:
                    row["is_default"] = False
                    if self._mem_default_id == profile_id:
                        self._mem_default_id = None
            row["updated_at"] = _utc_now()
            return LLMProfile.from_row(row)

        # --- postgres: динамический SET только по переданным полям ---
        sets: list[str] = []
        params: dict[str, Any] = {"id": profile_id}
        if name is not _UNSET:
            sets.append("name = :name"); params["name"] = final_name
        if provider is not _UNSET:
            sets.append("provider = :provider"); params["provider"] = final_provider
        if model is not _UNSET:
            sets.append("model = :model"); params["model"] = final_model
        if base_url is not _UNSET:
            sets.append("base_url = :base_url"); params["base_url"] = final_base_url
        if is_fallback is not _UNSET:
            sets.append("is_fallback = :is_fallback"); params["is_fallback"] = bool(is_fallback)
        if config is not _UNSET:
            sets.append("config = CAST(:config AS JSONB)")
            params["config"] = json.dumps(config or {}, ensure_ascii=False)
        if api_key is not _UNSET:
            sets.append(
                "api_key_encrypted = CASE WHEN :api_key = '' THEN NULL "
                "ELSE pgp_sym_encrypt(:api_key, :pgc_key, 'cipher-algo=aes256') END"
            )
            params["api_key"] = api_key or ""
            params["pgc_key"] = self.pgc_key
        if is_default is not _UNSET:
            sets.append("is_default = :is_default"); params["is_default"] = bool(is_default)
        sets.append("updated_at = now()")
        update_sql = text(
            f"UPDATE public.llm_profiles SET {', '.join(sets)} WHERE id = :id"
        )
        try:
            async with self.postgres.session() as session:
                # is_default=True → сначала снимаем флаг с остальных (атомарно
                # в той же транзакции, как в create/set_default).
                if is_default is not _UNSET and bool(is_default):
                    await session.execute(_CLEAR_DEFAULT_SQL, {"id": profile_id})
                await session.execute(update_sql, params)
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ProfileConflict(
                    f"profile name {final_name!r} already exists"
                ) from exc
            logger.warning("LLMProfileService.update failed: %s", exc)
            raise

        result = await self.get(profile_id=profile_id)
        if result is None:
            raise RuntimeError("profile update succeeded but reload failed")
        return result

    async def delete(self, *, profile_id: str) -> bool:
        if not profile_id:
            return False
        if self.postgres is None:
            removed = self._mem.pop(profile_id, None) is not None
            self._mem_keys.pop(profile_id, None)
            if self._mem_default_id == profile_id:
                self._mem_default_id = None
            return removed
        try:
            async with self.postgres.session() as session:
                await session.execute(_DELETE_SQL, {"id": profile_id})
            return True
        except Exception as exc:
            logger.warning("delete failed: %s", exc)
            return False

    async def record_health_check(self, *, profile_id: str, ok: bool) -> None:
        if not profile_id:
            return
        if self.postgres is None:
            row = self._mem.get(profile_id)
            if row is not None:
                row["last_health_check_at"] = _utc_now()
                row["last_health_ok"] = ok
            return
        try:
            async with self.postgres.session() as session:
                await session.execute(_TOUCH_HEALTH_SQL, {
                    "id": profile_id, "ok": ok,
                })
        except Exception as exc:
            logger.debug("record_health_check failed: %s", exc)


# === Health probe ==========================================================


async def probe_profile(
    *,
    profile: LLMProfile,
    api_key: Optional[str],
    timeout: float = 8.0,
) -> tuple[bool, Optional[str]]:
    """Health-check against profile endpoint.

    For OpenAI-compat: GET {base_url}/models — без отправки prompt.
    For TurboQuant: GET {base_url}/health/live — быстрее (W42a, см. turboquant_client.py).
    For Gemini managed: GET listModels endpoint.
    Returns (ok, error_message).
    """
    try:
        import httpx
    except ImportError:
        return False, "httpx not installed"

    if profile.provider == "gemini":
        if not api_key:
            return False, "missing api_key"
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, params={"key": api_key})
            if r.status_code == 200:
                return True, None
            return False, f"http {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    # W42a: TurboQuant имеет dedicated /health/live endpoint — быстрее и
    # не требует Bearer token (хотя мы его передаём если есть).
    if profile.provider == "turboquant":
        from backend.core.llm.turboquant_client import TurboQuantClient
        try:
            client = TurboQuantClient(
                base_url=profile.base_url or "",
                profile_id=profile.model,
                api_key=api_key,
            )
        except ValueError as exc:
            return False, str(exc)
        return await client.health_check()

    # OpenAI-compatible (openai / local_vllm / runpod).
    base = (profile.base_url or "").rstrip("/")
    if profile.provider == "openai" and not base:
        base = "https://api.openai.com/v1"
    if not base:
        return False, "base_url required"
    url = f"{base}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers)
        if r.status_code == 200:
            return True, None
        return False, f"http {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, str(exc)


# === Row helpers ===========================================================


async def _first_row(result: Any) -> Optional[dict[str, Any]]:
    # ВАЖНО: result.mappings().first() ПОТРЕБЛЯЕТ результат. Если строк нет
    # (напр. нет дефолтного llm_profile), он вернёт None — и НЕЛЬЗЯ дальше
    # звать result.first() снова: это "This result object is closed", которую
    # вызывающий код мисрепортил как «БД недоступна» и заваливал лог. Поэтому
    # путь mappings() самодостаточен: успех → строка-или-None, без второго чтения.
    try:
        m = result.mappings()
        row = m.first() if hasattr(m, "first") else None
        return dict(row) if row is not None else None
    except Exception:
        pass
    # Фолбэк ТОЛЬКО если mappings() сам бросил (а не «0 строк»).
    try:
        if hasattr(result, "first"):
            row = result.first()
            if row is None:
                return None
            if hasattr(row, "_mapping"):
                return dict(row._mapping)
            if isinstance(row, dict):
                return row
    except Exception:
        pass
    return None


async def _all_rows(result: Any) -> list[dict[str, Any]]:
    try:
        m = result.mappings()
        rows = m.all() if hasattr(m, "all") else []
        return [dict(r) for r in rows]
    except Exception:
        pass
    if hasattr(result, "all"):
        out = []
        for r in result.all():
            if hasattr(r, "_mapping"):
                out.append(dict(r._mapping))
            elif isinstance(r, dict):
                out.append(r)
        return out
    return []


__all__ = [
    "EXTERNAL_PROVIDERS",
    "OPENAI_COMPAT_PROVIDERS",
    "SUPPORTED_PROVIDERS",
    "LLMProfile",
    "LLMProfileService",
    "ProfileConflict",
    "ProfileNotFound",
    "ProfileValidationError",
    "probe_profile",
    "validate_profile_input",
]
