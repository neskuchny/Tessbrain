"""Backend registry — выбор executor backend по имени (W19).

`get_backend(name)` возвращает инстанс по строке (`noop` /
`claude_code_cli` / `cursor_cli` / `openhands`). Имена матчат
`Settings.executor_backend`.

Air-gap-aware: в enterprise mode только internal-friendly backends
(noop, openhands с internal URL). validate_enterprise() использует
`_is_internal_url` из W8.

Реалии:
- Не singleton-инстансы — каждый вызов возвращает свежий backend
  (некоторые backends держат httpx-client; per-request lifetime ок)
- Lazy-init: тяжёлые deps (httpx) импортируются только при создании
  инстанса
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.core.executors.base import ExecutorBackend, ExecutorError

logger = logging.getLogger(__name__)


_KNOWN_BACKENDS = {
    "noop", "claude_code_cli", "cursor_cli", "openhands", "codex_cli",
    "openworker",
}

# Backends которые делают внешние API-вызовы и НЕ могут работать в air-gap.
# codex_cli (Phase B.3): через ChatGPT Plus/Pro subscription или OpenAI API
# ходит наружу — НЕ для enterprise air-gap.
# openworker и openhands — свои сервисы на своём железе: в списке внешних
# их нет, но адрес обязан быть внутренним (validate_enterprise проверяет).
_EXTERNAL_BACKENDS = {"claude_code_cli", "cursor_cli", "codex_cli"}

# Дефолтный маршрут офисных типов задач: не код, а «собери бриф / письмо /
# сводку» — это профиль OpenWorker. Применяется ТОЛЬКО если его адрес задан:
# нет адреса — тип идёт в активный backend, как раньше. Явная карта
# EXECUTOR_TASK_ROUTES всегда сильнее.
_OFFICE_TASK_TYPES = {"office", "document", "email", "comms", "research",
                      "brief"}


def list_backend_names() -> list[str]:
    return sorted(_KNOWN_BACKENDS)


def is_external(backend: str) -> bool:
    """True если backend выходит в публичный интернет."""
    return backend in _EXTERNAL_BACKENDS


def get_backend(name: str) -> ExecutorBackend:
    """Создать инстанс backend'а по имени.

    Raises:
        ExecutorError: unknown name или невозможно сконструировать backend
                       (например, OpenHands без OPENHANDS_BASE_URL).
    """
    name = (name or "").strip().lower()
    if name == "noop":
        from backend.core.executors.backends.noop import NoopExecutor
        return NoopExecutor()
    if name == "claude_code_cli":
        from backend.core.executors.backends.claude_code import ClaudeCodeCLIExecutor
        return ClaudeCodeCLIExecutor()
    if name == "cursor_cli":
        from backend.core.executors.backends.cursor import CursorCLIExecutor
        return CursorCLIExecutor()
    if name == "openhands":
        from backend.core.executors.backends.openhands import OpenHandsExecutor
        return OpenHandsExecutor()
    if name == "codex_cli":
        from backend.core.executors.backends.codex import CodexCLIExecutor
        return CodexCLIExecutor()
    if name == "openworker":
        from backend.core.executors.backends.openworker import (
            OpenWorkerExecutor,
        )
        return OpenWorkerExecutor()
    raise ExecutorError(
        f"unknown executor backend {name!r}. Known: {sorted(_KNOWN_BACKENDS)}"
    )


def get_active_backend(*, override: Optional[str] = None) -> ExecutorBackend:
    """Создать backend из Settings.executor_backend (или override).

    Default — `noop` если ничего не задано.
    """
    if override:
        name = override
    else:
        try:
            from backend.config import get_settings
            name = getattr(get_settings(), "executor_backend", None) or "noop"
        except Exception:
            name = "noop"
    return get_backend(name)


def resolve_backend_name_for_task_type(
    task_type: Optional[str], *, override: Optional[str] = None,
) -> str:
    """Выбрать имя backend'а под тип задачи (vibe-tasking роутинг).

    Приоритет: явный override → карта типов (env EXECUTOR_TASK_ROUTES вида
    `landing=openhands,finmodel=codex_cli`) → активный backend из Settings →
    `noop`. Неизвестное имя из карты игнорируется (падаем на активный).

    Причина: разные задачи лучше отдавать разным исполнителям (лендинг —
    web-агенту, код — кодинг-CLI). Пока карта не задана, всё идёт в активный.
    """
    if override and override.strip():
        return override.strip().lower()

    tt = (task_type or "").strip().lower()
    if tt:
        import os
        raw = os.getenv("EXECUTOR_TASK_ROUTES", "") or ""
        for pair in raw.split(","):
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            if k.strip().lower() == tt:
                cand = v.strip().lower()
                if cand in _KNOWN_BACKENDS:
                    return cand
                logger.warning(
                    "EXECUTOR_TASK_ROUTES: unknown backend %r for type %r (ignored)",
                    cand, tt)
                break
        # Офисные типы → OpenWorker, если он поднят. Кодинг-агенту такие
        # задачи отдавать бессмысленно — он попытается написать код.
        if tt in _OFFICE_TASK_TYPES and (os.getenv("OPENWORKER_BASE_URL")
                                         or "").strip():
            return "openworker"

    try:
        from backend.config import get_settings
        return getattr(get_settings(), "executor_backend", None) or "noop"
    except Exception:
        return "noop"


def validate_enterprise(*, backend_name: str, openhands_base_url: str) -> list[str]:
    """Проверить совместимость с enterprise mode.

    Возвращает список violations (пустой = всё ОК).
    """
    violations: list[str] = []
    if backend_name in _EXTERNAL_BACKENDS:
        violations.append(
            f"executor_backend={backend_name!r} is managed-API; "
            "use 'openhands' or 'noop' in enterprise mode"
        )
    if backend_name == "openhands":
        # URL должен быть internal.
        from backend.config import _is_internal_url
        url = (openhands_base_url or "").strip()
        if not url:
            violations.append(
                "OPENHANDS_BASE_URL is required for openhands backend in enterprise mode"
            )
        elif not _is_internal_url(url):
            violations.append(
                f"OPENHANDS_BASE_URL={url!r} is not internal"
            )
    if backend_name == "openworker":
        # Свой сервис на своём железе — допустим в контуре, но только
        # по внутреннему адресу: офисный исполнитель с коннекторами
        # наружу через публичный URL был бы дырой в периметре.
        import os
        from backend.core.security.perimeter import is_internal_url
        url = (os.getenv("OPENWORKER_BASE_URL") or "").strip()
        if not url:
            violations.append(
                "OPENWORKER_BASE_URL is required for openworker backend "
                "in enterprise mode"
            )
        elif not is_internal_url(url):
            violations.append(
                f"OPENWORKER_BASE_URL={url!r} is not internal"
            )
    return violations


__all__ = [
    "get_active_backend",
    "get_backend",
    "is_external",
    "list_backend_names",
    "resolve_backend_name_for_task_type",
    "validate_enterprise",
]
