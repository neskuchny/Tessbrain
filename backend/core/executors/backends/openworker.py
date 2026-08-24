# -*- coding: utf-8 -*-
"""OpenWorker — исполнитель ОФИСНЫХ задач (не кодинга).

Закрывает дыру, найденную при разборе слоя исполнителей: все наши
backends — кодинг-агенты. А типичная задача после таскера — не код:
«собери бриф по клиенту», «подготовь письмо с цифрами», «разбери и
сведи в таблицу». OpenWorker (открытый агент-«сотрудник», MIT) делает
ровно это: у него коннекторы к офисным системам и петля «описал
результат → получил готовое», причём действия с последствиями у него
на его стороне закрыты подтверждением.

Почему это ИСПОЛНИТЕЛЬ, а не профиль модели — принципиально. У него
OpenAI-совместимый endpoint, и соблазн завести его «как модель» велик.
Но тогда каждый вызов модели в системе унаследовал бы его инструменты
(Slack, Jira, почта) В ОБХОД всех наших сит — среза, редакции, аудита.
Исполнитель же получает ЗАДАЧУ с проверками приёмки, а не поток промптов.

Топология: headless `openworker-server` на своей машине/VPS, обращение
по OPENWORKER_BASE_URL с токеном OPENWORKER_TOKEN (заголовок
X-OpenWorker-Token). Локальный сервис → в закрытом контуре допустим,
но адрес обязан быть внутренним (validate_enterprise это проверяет).

Модель выполнения: его совместимый endpoint синхронный, а наш контракт —
submit/poll. Поэтому submit стартует фоновую задачу в процессе API,
статус и результат живут в store (как у остальных backends) — поллинг
работает из любого запроса. Упал процесс до завершения — статус честно
останется RUNNING и протухнет по таймауту, а не притворится успехом.

Конфигурация:
- OPENWORKER_BASE_URL   — http://openworker:8765 (обязателен)
- OPENWORKER_TOKEN      — токен сервера (опционален, если сервер без auth)
- OPENWORKER_MODEL      — имя модели для его endpoint (опционально)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from backend.core.executors.base import (
    ExecutorBackend,
    ExecutorError,
    TaskHandle,
    TaskResult,
    TaskStatus,
    TaskSubmission,
)
from backend.core.executors.store import (
    get_handle,
    get_result as load_result,
    save_handle,
    save_result,
    update_status,
)

logger = logging.getLogger(__name__)

# Фоновые задачи держим за ссылку — иначе GC может убить их до завершения
# (тот же приём, что у knowledge_sync._SYNC_TASKS).
_RUNNING: set = set()


def _base_url() -> str:
    return (os.environ.get("OPENWORKER_BASE_URL") or "").rstrip("/")


class OpenWorkerExecutor(ExecutorBackend):
    name = "openworker"

    def __init__(self, *, base_url: Optional[str] = None,
                 token: Optional[str] = None,
                 model: Optional[str] = None) -> None:
        self.base_url = (base_url or _base_url()).rstrip("/")
        self.token = token or os.environ.get("OPENWORKER_TOKEN") or ""
        self.model = model or os.environ.get("OPENWORKER_MODEL") or ""
        if not self.base_url:
            raise ExecutorError(
                "OPENWORKER_BASE_URL not set; поднимите openworker-server "
                "и укажите его адрес")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-OpenWorker-Token"] = self.token
        return h

    async def submit(self, submission: TaskSubmission) -> TaskHandle:
        try:
            import httpx  # noqa: F401 — проверяем доступность до старта фона
        except ImportError as exc:
            raise ExecutorError("httpx not installed") from exc

        handle = TaskHandle.new(
            backend=self.name,
            user_id=submission.metadata.get("user_id"),
            tenant_id=submission.metadata.get("tenant_id"),
            working_dir=submission.working_dir,
            task_type=submission.task_type,
        )
        handle.status = TaskStatus.RUNNING
        await save_handle(handle)

        task = asyncio.create_task(self._run(handle.id, submission))
        _RUNNING.add(task)
        task.add_done_callback(_RUNNING.discard)
        return handle

    async def _run(self, handle_id: str, submission: TaskSubmission) -> None:
        """Фоновое выполнение: один вызов совместимого endpoint'а.

        Любой исход — включая сетевой сбой — заканчивается результатом в
        store: success=False с текстом ошибки, а не исчезнувшей задачей.
        """
        import httpx

        payload: dict[str, Any] = {
            "messages": [{
                "role": "user",
                "content": submission.tz_markdown,
            }],
            "stream": False,
        }
        if self.model:
            payload["model"] = self.model

        text, error = "", ""
        timeout = max(60, int(submission.timeout_seconds or 3600))
        try:
            async with httpx.AsyncClient(timeout=float(timeout)) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload, headers=self._headers())
            if resp.status_code >= 400:
                error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            else:
                data = resp.json()
                choices = data.get("choices") or []
                if choices:
                    text = str(((choices[0] or {}).get("message") or {})
                               .get("content") or "")
                if not text:
                    error = "пустой ответ исполнителя"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        ok = bool(text) and not error
        result = TaskResult(
            handle_id=handle_id,
            status=TaskStatus.DONE if ok else TaskStatus.FAILED,
            success=ok,
            summary=(text[:200] if ok else error[:200]),
            artifacts=([{"name": "result.md", "kind": "text",
                         "content": text}] if ok else []),
            logs_excerpt=error[:1000],
            error_message=None if ok else error,
        )
        await save_result(result)
        await update_status(handle_id,
                            TaskStatus.DONE if ok else TaskStatus.FAILED)
        if not ok:
            logger.warning("openworker task %s failed: %s", handle_id,
                           error[:200])

    async def get_status(self, handle: TaskHandle) -> TaskStatus:
        fresh = await get_handle(handle.id)
        return fresh.status if fresh else TaskStatus.UNKNOWN

    async def get_result(self, handle: TaskHandle) -> Optional[TaskResult]:
        raw = await load_result(handle.id)
        if not raw:
            return None
        try:
            status = TaskStatus(raw.get("status", TaskStatus.UNKNOWN.value))
        except ValueError:
            status = TaskStatus.UNKNOWN
        return TaskResult(
            handle_id=raw["handle_id"],
            status=status,
            success=bool(raw.get("success", False)),
            finished_at=raw.get("finished_at", ""),
            summary=raw.get("summary", ""),
            artifacts=list(raw.get("artifacts") or []),
            logs_excerpt=raw.get("logs_excerpt", ""),
            error_message=raw.get("error_message"),
        )

    async def cancel(self, handle: TaskHandle) -> bool:
        # Совместимый endpoint не даёт ручки отмены запущенного прогона —
        # честно говорим «нет», а не помечаем отменённым то, что доедет.
        return False
