"""OpenHands HTTP executor (W19, full implementation in W20).

OpenHands runtime exposes REST API (default `:3000/api/conversations`).
Здесь — minimal HTTP-клиент: submit POST'ит conversation request и
возвращает handle с `backend_ref = conversation_id`. Polling статуса
через `GET /api/conversations/{id}`.

Дизайн:
- Этот класс — production-ready submit + status, но extra polish
  (artifact collection из OpenHands sandbox, streaming events) запланирован
  на W20 deep-integration PR.
- Air-gap-ready: проверяет что endpoint internal через `_is_internal_url`
  из W8. Validator в registry форсит это в enterprise mode.

Конфигурация:
- `OPENHANDS_BASE_URL` (e.g. http://openhands:3000)
- `OPENHANDS_API_KEY` (если runtime требует auth)
- `OPENHANDS_LLM_MODEL` (имя модели для conversation, e.g. `qwen-3.6-27b`)
"""
from __future__ import annotations

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
    save_handle,
    save_result,
    update_status,
)

logger = logging.getLogger(__name__)


def _resolve_base_url() -> str:
    return (os.environ.get("OPENHANDS_BASE_URL") or "").rstrip("/")


def _resolve_api_key() -> Optional[str]:
    return os.environ.get("OPENHANDS_API_KEY") or None


def _resolve_model() -> Optional[str]:
    return os.environ.get("OPENHANDS_LLM_MODEL") or None


def _map_oh_status(status: str) -> TaskStatus:
    """Привести OpenHands `state` к нашему TaskStatus."""
    s = (status or "").lower()
    if s in {"queued", "pending", "starting"}:
        return TaskStatus.QUEUED
    if s in {"running", "in_progress", "active"}:
        return TaskStatus.RUNNING
    if s in {"finished", "done", "completed", "success"}:
        return TaskStatus.DONE
    if s in {"error", "failed", "crashed"}:
        return TaskStatus.FAILED
    if s in {"cancelled", "canceled", "stopped"}:
        return TaskStatus.CANCELLED
    return TaskStatus.UNKNOWN


class OpenHandsExecutor(ExecutorBackend):
    name = "openhands"

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or _resolve_base_url()).rstrip("/")
        self.api_key = api_key or _resolve_api_key()
        self.model = model or _resolve_model()
        if not self.base_url:
            raise ExecutorError(
                "OPENHANDS_BASE_URL not set; OpenHands executor requires it"
            )

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def submit(self, submission: TaskSubmission) -> TaskHandle:
        try:
            import httpx
        except ImportError as exc:
            raise ExecutorError("httpx not installed") from exc

        handle = TaskHandle.new(
            backend=self.name,
            user_id=submission.metadata.get("user_id"),
            tenant_id=submission.metadata.get("tenant_id"),
            working_dir=submission.working_dir,
            task_type=submission.task_type,
        )

        # OpenHands API: POST /api/conversations
        # body: {initial_user_msg, llm_model, max_iterations, ...}
        body: dict[str, Any] = {
            "initial_user_msg": submission.tz_markdown,
            "max_iterations": 50,
        }
        if self.model:
            body["llm_model"] = self.model
        if submission.callback_url:
            body["webhook_url"] = submission.callback_url

        url = f"{self.base_url}/api/conversations"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ExecutorError(f"OpenHands submit network error: {exc}") from exc

        if resp.status_code >= 400:
            raise ExecutorError(
                f"OpenHands submit HTTP {resp.status_code}: {resp.text[:500]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ExecutorError(f"OpenHands invalid JSON: {exc}") from exc

        conversation_id = (
            data.get("conversation_id") or data.get("id") or data.get("session_id")
        )
        if not conversation_id:
            raise ExecutorError("OpenHands response missing conversation_id")

        handle.backend_ref = str(conversation_id)
        handle.status = TaskStatus.QUEUED
        await save_handle(handle)
        return handle

    async def get_status(self, handle: TaskHandle) -> TaskStatus:
        if not handle.backend_ref:
            return TaskStatus.UNKNOWN
        try:
            import httpx
        except ImportError:
            return TaskStatus.UNKNOWN
        url = f"{self.base_url}/api/conversations/{handle.backend_ref}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("OpenHands get_status network error: %s", exc)
            return handle.status
        if resp.status_code >= 400:
            return TaskStatus.UNKNOWN
        try:
            data = resp.json()
        except Exception:
            return TaskStatus.UNKNOWN
        oh_status = data.get("status") or data.get("state") or ""
        new_status = _map_oh_status(oh_status)
        if new_status != handle.status:
            await update_status(handle.id, new_status)
        return new_status

    async def get_result(self, handle: TaskHandle) -> Optional[TaskResult]:
        # Если результат уже сохранён через webhook — просто вернём.
        from backend.core.executors.store import get_result as load_result
        raw = await load_result(handle.id)
        if raw:
            return TaskResult(
                handle_id=raw["handle_id"],
                status=TaskStatus(raw.get("status", TaskStatus.UNKNOWN.value)),
                success=bool(raw.get("success", False)),
                finished_at=raw.get("finished_at", ""),
                summary=raw.get("summary", ""),
                artifacts=list(raw.get("artifacts") or []),
                logs_excerpt=raw.get("logs_excerpt", ""),
                error_message=raw.get("error_message"),
            )

        # Иначе pull из API: финальные сообщения + collected files.
        if not handle.backend_ref:
            return None
        try:
            import httpx
        except ImportError:
            return None
        url = f"{self.base_url}/api/conversations/{handle.backend_ref}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("OpenHands get_result network error: %s", exc)
            return None
        if resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except Exception:
            return None

        oh_status = _map_oh_status(data.get("status") or data.get("state") or "")
        if oh_status not in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return None

        success = oh_status == TaskStatus.DONE
        last_message = ""
        messages = data.get("messages") or []
        if isinstance(messages, list) and messages:
            last_message = str(messages[-1].get("content") or "")[:4000]

        # W20: collect artifacts из sandbox (best-effort).
        artifacts: list[dict[str, Any]] = []
        if success and handle.backend_ref:
            try:
                from backend.core.executors.backends.openhands_artifacts import (
                    collect_artifacts,
                )
                collected = await collect_artifacts(
                    base_url=self.base_url,
                    conversation_id=handle.backend_ref,
                    api_key=self.api_key,
                )
                artifacts = [a.to_dict() for a in collected]
            except Exception as exc:
                logger.warning("OpenHands artifact collection failed: %s", exc)

        result = TaskResult(
            handle_id=handle.id,
            status=oh_status,
            success=success,
            summary=f"OpenHands conversation {handle.backend_ref} finished ({len(artifacts)} artifacts)",
            artifacts=artifacts,
            logs_excerpt=last_message,
            error_message=None if success else (data.get("error") or "OpenHands failed"),
            raw=data,
        )
        await save_result(result)
        return result

    async def cancel(self, handle: TaskHandle) -> bool:
        if not handle.backend_ref:
            return False
        try:
            import httpx
        except ImportError:
            return False
        url = f"{self.base_url}/api/conversations/{handle.backend_ref}/stop"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("OpenHands cancel network error: %s", exc)
            return False
        ok = resp.status_code < 400
        if ok:
            await update_status(handle.id, TaskStatus.CANCELLED)
        return ok


__all__ = ["OpenHandsExecutor", "_map_oh_status"]
