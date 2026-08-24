# -*- coding: utf-8 -*-
"""Периметр развёртывания: одно определение «внутреннего» на весь продукт.

Зачем отдельный модуль. Правило «этот адрес внутри контура» проверяется
в четырёх местах: валидатор Settings при старте, роутер моделей, загрузчик
внешних ссылок, веб-поиск. Пока определение было записано в каждом месте
своё, air-gap держался только там, где о нём вспомнили: боевой обход
нашёлся именно так — переменные окружения проверялись, а профиль модели
из базы шёл мимо проверки и мог указывать на публичное облако.

Модуль намеренно на голой стандартной библиотеке: он импортируется из
`backend.config`, поэтому не может тянуть ни настройки, ни клиентов.

Главные функции:
  is_internal_url(url)                  — адрес внутри контура?
  enterprise_mode_enabled()             — включён ли закрытый контур
  llm_target_leaves_perimeter(...)      — уйдёт ли этот вызов модели наружу
"""
from __future__ import annotations

import ipaddress
import os
from typing import Optional
from urllib.parse import urlparse

# Доменные суффиксы, которые в on-prem-развёртывании заведомо внутренние.
INTERNAL_HOST_SUFFIXES = (
    ".local", ".internal", ".lan", ".home", ".corp",
    ".intranet", ".svc.cluster.local", ".svc", ".cluster.local",
)


def is_internal_url(url: str) -> bool:
    """True, если url указывает на internal/on-prem network.

    Допустимы:
    - localhost / 127.0.0.0/8 / ::1
    - RFC1918: 10/8, 172.16/12, 192.168/16
    - link-local: 169.254/16, fe80::/10
    - kubernetes service-DNS: *.svc.cluster.local, *.svc, *.cluster.local
    - .local / .internal / .lan / .home / .corp

    Любые публичные IP или TLD (.com, .net, ...) трактуются как external —
    air-gap-валидатор их отвергает. Пустой адрес — не внутренний: «не знаю,
    куда пойдёт» приравнивается к «наружу».
    """
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False

    # Простые имена без точек (docker-compose, k8s short-names) считаем
    # internal — резолвятся внутри сети (e.g. "vllm", "ollama").
    if "." not in host:
        return True

    # IP literal.
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        pass

    return host.endswith(INTERNAL_HOST_SUFFIXES)


# Провайдеры, чей SDK ходит в публичное облако по зашитому адресу: своего
# base_url у них нет, поэтому «направить внутрь» их нельзя в принципе.
NATIVE_PUBLIC_PROVIDERS = frozenset({
    "gemini",       # google genai SDK
    "anthropic",    # anthropic SDK
    "claude_cli",   # CLI с подпиской — ходит в api.anthropic.com
    "codex_cli",    # то же для OpenAI
})

# Провайдеры, которые обращаются к чужой инфраструктуре. Локальные
# (local_vllm, ollama, turboquant) сюда не входят. runpod — входит: это
# аренда GPU в чужом облаке, трафик выходит из контура.
EXTERNAL_LLM_PROVIDERS = frozenset({
    "gemini", "openai", "runpod",
    "deepseek", "qwen", "openrouter", "together", "mistral", "groq",
    "anthropic", "xai", "yandex", "kimi",
    "claude_cli", "codex_cli",
})


def enterprise_mode_enabled(settings: object = None) -> bool:
    """Включён ли закрытый контур (air-gap).

    Источник — переменная окружения ENTERPRISE_MODE; если её нет, читаем
    `Settings.enterprise_mode` из переданного объекта настроек.

    Режим по умолчанию выключен, и при недоступности настроек мы отвечаем
    «выключен». Это не дыра: закрытый контур включается сознательно, и в
    момент включения валидатор Settings не даёт процессу стартовать при
    нарушении правил — то есть в самом контуре ответ всегда положительный.
    """
    raw = os.environ.get("ENTERPRISE_MODE", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    if settings is not None:
        try:
            return bool(getattr(settings, "enterprise_mode", False))
        except Exception:
            return False
    try:
        from backend.config import get_settings
        return bool(getattr(get_settings(), "enterprise_mode", False))
    except Exception:
        return False


def llm_target_leaves_perimeter(
    provider: Optional[str],
    resolved_base_url: Optional[str],
) -> Optional[str]:
    """Уйдёт ли вызов этой модели за периметр? Причина словами или None.

    Отказ по умолчанию: неизвестный провайдер без адреса — это «неизвестно
    куда», и в закрытом контуре такой вызов не выпускается. Ответ —
    причина текстом, чтобы администратор увидел, что именно не так, а не
    молчаливое «недоступно».

    `resolved_base_url` — адрес ПОСЛЕ подстановки дефолтов провайдера
    (у клиента это `_resolved_base_url()`). Мы намеренно не держим свою
    копию таблицы дефолтных адресов: две таблицы разошлись бы, и проверка
    периметра стала бы проверять не то, куда реально уходит запрос.
    """
    p = (provider or "").strip().lower()
    if not p:
        return "провайдер не указан — неизвестно, куда уйдёт запрос"
    if p in NATIVE_PUBLIC_PROVIDERS:
        return (
            f"провайдер {p} ходит в публичное облако своим SDK — "
            "перенаправить его внутрь контура нельзя"
        )
    url = (resolved_base_url or "").strip()
    if not url:
        return (
            f"у провайдера {p} не задан адрес — неизвестно, куда уйдёт запрос"
        )
    if not is_internal_url(url):
        return f"адрес {url} ведёт за пределы контура"
    return None


__all__ = [
    "EXTERNAL_LLM_PROVIDERS",
    "INTERNAL_HOST_SUFFIXES",
    "NATIVE_PUBLIC_PROVIDERS",
    "enterprise_mode_enabled",
    "is_internal_url",
    "llm_target_leaves_perimeter",
]
