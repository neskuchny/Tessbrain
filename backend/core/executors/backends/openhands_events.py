"""OpenHands event streaming (W20).

Pull-based events poller через `GET /api/conversations/{id}/events`. Async
generator который выдаёт события по мере их появления. Останавливается
когда conversation finished или достигнут timeout.

OpenHands API возвращает events в виде списка `{id, source, action,
timestamp, message, ...}`. Мы yield'им raw dicts — caller сам решает
как форматировать (UI / logs / stream к юзеру).

Использование (например в WS endpoint):

    async for event in stream_events(base_url=..., conversation_id=..., api_key=...):
        await ws.send_json(event)

Дизайн:
- Pull, а не SSE — большинство OpenHands версий не имеют SSE.
- Tracks `last_event_id` чтобы не дублировать.
- Автостоп при `event.action == 'finish'` или статусе done/failed.
- Graceful: network errors → пропускаем итерацию, продолжаем poll.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


_DEFAULT_POLL_INTERVAL = 1.5    # сек
_DEFAULT_TIMEOUT = 3600          # max 1ч на conversation
_TERMINAL_ACTIONS = frozenset({"finish", "stop", "error", "abort"})


async def stream_events(
    *,
    base_url: str,
    conversation_id: str,
    api_key: Optional[str] = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: int = _DEFAULT_TIMEOUT,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator который выдаёт events по мере появления.

    Завершается когда:
    - conversation finished (terminal action в events)
    - истёк timeout
    - произошла unrecoverable ошибка (network через 5+ попыток)
    """
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not installed; cannot stream events")
        return

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    url = f"{base_url.rstrip('/')}/api/conversations/{conversation_id}/events"

    last_event_id: Optional[str] = None
    consecutive_errors = 0
    deadline = asyncio.get_event_loop().time() + timeout

    async with httpx.AsyncClient(timeout=10.0, headers=headers or {}) as client:
        while asyncio.get_event_loop().time() < deadline:
            params = {}
            if last_event_id is not None:
                params["since"] = last_event_id

            try:
                resp = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                consecutive_errors += 1
                logger.debug("OpenHands events poll error: %s", exc)
                if consecutive_errors >= 5:
                    logger.warning("OpenHands events: 5 consecutive errors, stopping")
                    return
                await asyncio.sleep(poll_interval)
                continue

            if resp.status_code == 404:
                logger.debug("OpenHands conversation %s not found", conversation_id)
                return
            if resp.status_code >= 400:
                consecutive_errors += 1
                await asyncio.sleep(poll_interval)
                continue

            consecutive_errors = 0
            try:
                data = resp.json()
            except Exception:
                await asyncio.sleep(poll_interval)
                continue

            events = _extract_events_list(data)
            terminal_seen = False

            for ev in events:
                # advance last_event_id даже если не yield'ит — чтобы next
                # poll не выдал повтор.
                ev_id = ev.get("id") or ev.get("event_id")
                if ev_id is not None:
                    last_event_id = str(ev_id)
                yield ev
                action = (ev.get("action") or "").lower()
                if action in _TERMINAL_ACTIONS:
                    terminal_seen = True

            if terminal_seen:
                return

            await asyncio.sleep(poll_interval)


def _extract_events_list(data: Any) -> list[dict[str, Any]]:
    """OpenHands возвращает события в разных схемах."""
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        for key in ("events", "items", "data"):
            v = data.get(key)
            if isinstance(v, list):
                return [e for e in v if isinstance(e, dict)]
    return []


__all__ = ["stream_events"]
