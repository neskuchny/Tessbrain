# -*- coding: utf-8 -*-
"""BYO-доступ помощника-сборщика на пользователя (свой аккаунт, не сервера).

Раньше сборка (Kanon-handoff → claude/cursor/codex CLI) шла на ОДИН серверный
аккаунт из окружения. Для SaaS это неправильно. Два SaaS-нативных способа
привязать СВОЙ доступ:

1. **Свой API-ключ** (`mode="api_key"`) — вставляешь токен в интерфейсе, сборка
   идёт на твой биллинг. Полностью в UI, без терминала.
2. **Своя подписка через свой CLI** (`mode="subscription"`) — у каждого
   пользователя ИЗОЛИРОВАННАЯ папка входа CLI (CLAUDE_CONFIG_DIR / CODEX_HOME):
   один раз `claude login` / `codex login` в своей папке (по подписке
   Claude Pro / ChatGPT Plus), дальше сборки автоматически идут через твою
   сессию. Подписки пользователей не смешиваются.

Почему НЕ «логин прямо в браузере внутри веба»: OAuth-флоу CLI пишет креды в
ФС; автоматизировать чужой браузер из веб-сервера нельзя. Но per-user
изоляция каталога входа + разовый `login` — рабочий и честный путь. ToS:
личная подписка = один человек (тот, кто вошёл в свою папку).

Хранение: per-user JSON под tenant_paths. API-ключ шифруется Fernet (обёртки
_enc/_dec над секретами интеграций), наружу — только маска. Подписка секрета
не хранит — только каталоги входа per-user.

Приоритет при сборке: личный доступ пользователя → серверный дефолт (env /
серверный `claude login`) — кто не привязал, работает по-старому.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

# provider → env-переменная API-ключа, которую понимает CLI/бэкенд
_PROVIDER_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY",),            # claude / cursor
    "openai": ("OPENAI_API_KEY", "CODEX_API_KEY"),  # codex / openai
    # OpenRouter — единый ключ ко МНОГИМ моделям (deepseek/qwen/kimi/glm/grok/
    # gemini/…): удобно для тиринга прогона встреч (base_url openrouter.ai/api/v1).
    "openrouter": ("OPENROUTER_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),  # gemini напрямую
    "deepseek": ("DEEPSEEK_API_KEY",),               # deepseek напрямую
    "xai": ("XAI_API_KEY",),                          # grok
    "qwen": ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),   # Qwen через Alibaba DashScope
}

# agent → (env-переменная изоляции каталога, файл-маркер «залогинен», команда)
_AGENT_CLI = {
    "claude": ("CLAUDE_CONFIG_DIR", ".credentials.json", "claude"),
    "codex": ("CODEX_HOME", "auth.json", "codex login"),
    # cursor-agent: документированной переменной изоляции нет → подписка не
    # поддержана (только API-ключ). claude/codex — поддержаны.
}

# agent → провайдер в Интеграциях (там пользователь уже вводит свой ключ).
# Сборка тянет его АВТОМАТИЧЕСКИ, если явного BYO-переопределения нет.
_AGENT_PROVIDER = {"claude": "anthropic", "cursor": "anthropic", "codex": "openai"}


def _path(user_id: str) -> str:
    from backend.core.store.tenant_paths import coding_credentials_path_for_user
    return coding_credentials_path_for_user(user_id)


def _home_base(user_id: str) -> str:
    from backend.core.store.tenant_paths import coding_home_dir_for_user
    return coding_home_dir_for_user(user_id)


def _agent_dir(user_id: str, agent: str) -> str:
    d = os.path.join(_home_base(user_id), agent)
    os.makedirs(d, exist_ok=True)
    return d


# Обёртки над шифрованием интеграций (ленивый импорт: не тянем Fernet при
# импорте модуля; тесты подменяют _enc/_dec, не трогая крипто-бэкенд).
def _enc(secret: str) -> str:
    from backend.api.routes.integrations import encrypt_secret
    return encrypt_secret(secret)


def _dec(enc: str) -> str:
    from backend.api.routes.integrations import decrypt_secret
    return decrypt_secret(enc)


def _mask(key: str) -> str:
    k = key or ""
    if len(k) <= 8:
        return "•" * len(k)
    return f"{k[:4]}…{k[-4:]}"


def _load(user_id: str) -> dict:
    path = _path(user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save(user_id: str, data: dict) -> None:
    path = _path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


# ── Режим 1: свой API-ключ ────────────────────────────────────────────────

def set_key(user_id: str, provider: str, api_key: str) -> dict:
    """Сохранить (зашифровать) свой API-ключ. Возвращает статус без plaintext."""
    provider = (provider or "").strip().lower()
    api_key = (api_key or "").strip()
    if provider not in _PROVIDER_ENV:
        return {"error": f"provider must be one of {sorted(_PROVIDER_ENV)}"}
    if not api_key:
        return {"error": "api_key is required"}
    _save(user_id, {"mode": "api_key", "provider": provider,
                    "key_enc": _enc(api_key), "masked": _mask(api_key)})
    return {"mode": "api_key", "has_key": True, "provider": provider,
            "masked": _mask(api_key)}


# ── Режим 2: своя подписка через свой CLI ─────────────────────────────────

def set_subscription(user_id: str) -> dict:
    """Переключить пользователя в режим «своя подписка/свой CLI»: заводим
    изолированные каталоги входа. Логин делается разово командой (login_info)."""
    _save(user_id, {"mode": "subscription"})
    return get_status(user_id)


def _agent_connected(user_id: str, agent: str) -> bool:
    conf = _AGENT_CLI.get(agent)
    if not conf:
        return False
    _env, marker, _cmd = conf
    return os.path.exists(os.path.join(_home_base(user_id), agent, marker))


def login_info(user_id: str, agent: str) -> dict:
    """Каталог + разовая команда входа по подписке для агента + статус."""
    conf = _AGENT_CLI.get(agent)
    if not conf:
        return {"error": f"подписка не поддержана для '{agent}' (только {sorted(_AGENT_CLI)})"}
    env_var, _marker, cmd = conf
    d = _agent_dir(user_id, agent)
    return {"agent": agent, "dir": d, "connected": _agent_connected(user_id, agent),
            "command": f"{env_var}={d} {cmd}"}


# ── Общий статус / удаление ───────────────────────────────────────────────

def get_status(user_id: str) -> dict:
    """Текущий режим и статус (маскированно; plaintext никогда не отдаём)."""
    d = _load(user_id)
    mode = d.get("mode")
    if mode == "api_key":
        return {"mode": "api_key", "has_key": True,
                "provider": d.get("provider"), "masked": d.get("masked") or "••••"}
    if mode == "subscription":
        agents = {a: {"connected": _agent_connected(user_id, a),
                      "command": login_info(user_id, a).get("command")}
                  for a in _AGENT_CLI}
        return {"mode": "subscription", "has_key": True, "agents": agents}
    return {"mode": None, "has_key": False}


def delete_key(user_id: str) -> dict:
    """Отвязать личный доступ (ключ/режим) — сборка вернётся на серверный
    дефолт. Каталоги входа не трогаем (чтобы не терять логин при пере-привязке)."""
    path = _path(user_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        logger.debug("delete_key failed", exc_info=True)
    return {"mode": None, "has_key": False}


# ── Разрешение окружения для subprocess сборки ────────────────────────────

def resolve_env(user_id: str, agent: Optional[str] = None) -> dict:
    """ЯВНЫЙ BYO-режим (свой ключ / своя подписка), заданный в SIMA. Синхронно.
    Ничего явного не привязано → {} (дальше пробуется Интеграции — см.
    resolve_env_for_build). В лог не пишем."""
    d = _load(user_id)
    mode = d.get("mode")
    if mode == "api_key":
        provider = d.get("provider") or ""
        if provider not in _PROVIDER_ENV:
            return {}
        key = _dec(d.get("key_enc") or "")
        if not key:
            return {}
        return {var: key for var in _PROVIDER_ENV[provider]}
    if mode == "subscription":
        conf = _AGENT_CLI.get(agent or "")
        if not conf:
            return {}  # cursor или неизвестный → нет изоляции, серверный дефолт
        env_var, _marker, _cmd = conf
        # SINGLE-USER / ЛОКАЛЬНАЯ установка: не изолируем каталог входа, а
        # используем ДЕФОЛТНЫЙ логин CLI (~/.claude), где пользователь уже сделал
        # `claude login` (и `claude -p` работает из терминала). Иначе handoff
        # указывает CLI на пустую tenant-папку → «Not logged in · run /login».
        # За флагом (дефолт OFF → прежняя мультитенант-изоляция для SaaS).
        _shared = (os.getenv("TESSENT_HANDOFF_SHARED_CLI_LOGIN", "") or "").lower() \
            in ("on", "true", "1")
        out = {} if _shared else {env_var: _agent_dir(user_id, agent)}
        # КЛЮЧЕВОЕ для «CLI по подписке»: если в окружении процесса торчит
        # серверный ANTHROPIC_API_KEY/OPENAI_API_KEY, CLI берёт ПЛАТНЫЙ API-ключ
        # (pay-as-you-go) и падает «Credit balance is too low» — вместо того чтобы
        # идти по подписке (claude login). Помечаем провайдерские ключи к
        # УДАЛЕНИЮ из окружения subprocess (пустое значение = unset, см.
        # _exec_handoff), чтобы CLI использовал именно подписку.
        provider = _AGENT_PROVIDER.get(agent or "")
        for var in _PROVIDER_ENV.get(provider or "", ()):
            out.setdefault(var, "")
        # anthropic-CLI также перехватывает ANTHROPIC_AUTH_TOKEN — тоже гасим
        if provider == "anthropic":
            out.setdefault("ANTHROPIC_AUTH_TOKEN", "")
        return out
    return {}


async def _integration_key(user_id: str, provider: str) -> str:
    """Расшифрованный api_key провайдера из Интеграций (там пользователь уже
    вводит ключи Anthropic/OpenAI). Never-raise → '' при отсутствии/ошибке."""
    try:
        from backend.api.routes.integrations import get_user_integration_keys
        keys = await get_user_integration_keys(user_id, provider) or {}
        return str(keys.get("api_key") or "").strip()
    except Exception:
        logger.debug("integration key lookup failed", exc_info=True)
        return ""


async def resolve_env_for_build(user_id: str, agent: Optional[str] = None) -> dict:
    """Полное разрешение окружения сборки. Приоритет:
      1) явный BYO в SIMA (свой ключ / своя подписка);
      2) ключ из Интеграций провайдера этого агента (claude/cursor→anthropic,
         codex→openai) — «добавил ключ в Интеграциях → сборка его тянет»;
      3) {} → серверный дефолт.
    """
    explicit = resolve_env(user_id, agent)
    if explicit:
        return explicit
    provider = _AGENT_PROVIDER.get(agent or "")
    if provider and provider in _PROVIDER_ENV:
        key = await _integration_key(user_id, provider)
        if key:
            return {var: key for var in _PROVIDER_ENV[provider]}
    return {}


async def build_credential_status(user_id: str) -> dict:
    """Статус доступа для сборки: явный BYO ИЛИ ключ из Интеграций. Для UI."""
    st = get_status(user_id)
    if st.get("mode"):
        return st
    # нет явного BYO — смотрим Интеграции (anthropic/openai)
    for provider in ("anthropic", "openai"):
        key = await _integration_key(user_id, provider)
        if key:
            return {"mode": "integration", "has_key": True,
                    "provider": provider, "masked": _mask(key)}
    return {"mode": None, "has_key": False}
