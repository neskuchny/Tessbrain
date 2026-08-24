# -*- coding: utf-8 -*-
"""Signals WebSocket — real-time pulse для wow-screen на фронте.

Зачем: «движение = жизнь» (OSIRIS-урок). До этого фронт делал polling 30s
на /api/v1/meetflow/insights — все числа замерзают, ничего не пульсирует.
С WebSocket любой ingest event (LLM-вызов, новый insight, аномалия,
добавление узла в граф) → push клиентам → подсветка / пульс / тикающие
числа в реальном времени.

Архитектура:
  - `SignalManager` — отдельный broadcast-менеджер для signals (не
    мешаемся с `connection_manager` из chat_ws.py — другой контракт)
  - `SignalsWebSocketHandler` — Litestar WebsocketListener на `/ws/signals`
  - `publish_signal()` — fire-and-forget helper, синхронный snapshot
    события + asyncio.create_task для broadcast'а. Любой backend код может
    позвать без знаний про WS — если loop'а нет, событие просто drop'нется
    (telemetry-style: не ронять основной flow).

Tenant isolation: events публикуются с tenant_id, broadcast только
подписчикам того же тенанта. Без `tenant_id` (legacy) — рассылка всем
подключённым (для dev/test).

Контракт сообщений:
  {"type": "usage_tracked", "tenant_id": "...", "payload": {
     "model": "gemini-flash-latest", "cost": 0.0023, "meeting_id": "...",
     "surface": "ingest", "at": "2026-06-15T10:23:11+00:00"
  }}
  {"type": "insight_added", "tenant_id": "...", "payload": {...}}
  {"type": "anomaly_detected", "tenant_id": "...", "payload": {...}}
  {"type": "graph_node_added", "tenant_id": "...", "payload": {...}}
  {"type": "pulse", "ts": "...", "payload": {}}  — keepalive, без события
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from litestar import WebSocket
from litestar.exceptions import WebSocketDisconnect
from litestar.handlers import WebsocketListener

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """Real-time событие. type — короткий enum-like string, payload — свободный
    словарь, tenant_id — для изоляции broadcasting'а."""
    type: str
    payload: Dict[str, Any]
    tenant_id: Optional[str] = None
    ts: str = field(default_factory=lambda:
                    datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class SignalManager:
    """Менеджер подписчиков на сигналы.

    Отдельный от chat-WS connection_manager намеренно — у них разный
    контракт сообщений (chat шлёт persistent state, signals — событийный
    push). Per-connection storage, чтобы клиент мог фильтровать на стороне
    подписки (subscribe к подмножеству event-types).
    """

    def __init__(self):
        # connection_id -> {socket, user_id, tenant_id, subscribed_types}
        self._conns: Dict[str, Dict[str, Any]] = {}
        # tenant_id -> set of connection_ids (для O(1) broadcast)
        self._by_tenant: Dict[str, Set[str]] = {}
        # Для broadcasting без блокировки caller'а (track_usage синхронный)
        self._queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=10_000)
        self._dispatcher_task: Optional[asyncio.Task] = None

    async def connect(
        self,
        socket: WebSocket,
        connection_id: str,
        *,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> None:
        await socket.accept()
        self._conns[connection_id] = {
            "socket": socket,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "subscribed": None,   # None = all types; set = whitelist
        }
        if tenant_id:
            self._by_tenant.setdefault(tenant_id, set()).add(connection_id)
        # Ленивый старт dispatcher'а (первое подключение поднимает loop-task)
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(self._dispatch_loop())
        logger.info("Signals WS connected: %s (user=%s tenant=%s)",
                    connection_id, user_id, tenant_id)

    def disconnect(self, connection_id: str) -> None:
        info = self._conns.pop(connection_id, None)
        if not info:
            return
        tenant_id = info.get("tenant_id")
        if tenant_id and tenant_id in self._by_tenant:
            self._by_tenant[tenant_id].discard(connection_id)
            if not self._by_tenant[tenant_id]:
                del self._by_tenant[tenant_id]
        logger.info("Signals WS disconnected: %s", connection_id)

    def set_subscription(
        self, connection_id: str, types: Optional[Set[str]],
    ) -> None:
        """Клиент может прислать {"action":"subscribe","types":["..."]}.
        None — снять фильтр (получать всё)."""
        if connection_id in self._conns:
            self._conns[connection_id]["subscribed"] = types

    def publish_nowait(self, signal: Signal) -> bool:
        """Sync push в очередь. Без блокировки. False если очередь полна
        (drop event'а — telemetry не должна тормозить основной flow)."""
        try:
            self._queue.put_nowait(signal)
            return True
        except asyncio.QueueFull:
            # Троттлим лог: при шторме событий предупреждение на КАЖДЫЙ drop
            # само становилось флудом (сотни строк/сек).
            self._dropped = getattr(self, "_dropped", 0) + 1
            if self._dropped % 500 == 1:
                logger.warning("Signals queue full, dropped %d (last: %s)",
                               self._dropped, signal.type)
            return False

    async def _dispatch_loop(self) -> None:
        """Фоновый task: разгребает очередь и рассылает по WS."""
        logger.info("Signals dispatcher started")
        while True:
            try:
                signal = await self._queue.get()
                await self._broadcast(signal)
            except asyncio.CancelledError:
                logger.info("Signals dispatcher cancelled")
                raise
            except Exception:
                # Никогда не убиваем dispatcher из-за одного битого события
                logger.exception("Signals dispatch failed")

    async def _broadcast(self, signal: Signal) -> None:
        """Кому слать: если tenant_id есть — только подписчикам тенанта.
        Событие БЕЗ tenant_id уходит только соединениям без тенанта (dev):
        рассылка «всем подключённым» была утечкой — чужие payload'ы
        (инсайты, meeting_id, стоимость) уходили любому открытому сокету.
        Уважаем per-connection type-фильтр."""
        if signal.tenant_id and signal.tenant_id in self._by_tenant:
            target_ids = list(self._by_tenant[signal.tenant_id])
        elif signal.tenant_id:
            # Тенант указан, но подписчиков нет — пропускаем
            return
        else:
            target_ids = [cid for cid, info in self._conns.items()
                          if not info.get("tenant_id")]

        payload = signal.to_json()
        for cid in target_ids:
            info = self._conns.get(cid)
            if not info:
                continue
            subscribed = info.get("subscribed")
            if subscribed is not None and signal.type not in subscribed:
                continue
            socket: WebSocket = info["socket"]
            try:
                await socket.send_text(payload)
            except (WebSocketDisconnect, Exception) as e:
                logger.debug("Signals send failed to %s: %s — disconnecting", cid, e)
                self.disconnect(cid)


# Глобальный singleton — нужен для fire-and-forget из track_usage()
_manager: Optional[SignalManager] = None


def get_signal_manager() -> SignalManager:
    global _manager
    if _manager is None:
        _manager = SignalManager()
    return _manager


def publish_signal(
    type: str,
    payload: Dict[str, Any],
    *,
    tenant_id: Optional[str] = None,
) -> None:
    """Fire-and-forget публикация события. Синхронная, безопасна для
    вызова из ЛЮБОГО места backend'а (включая sync-контексты типа
    UsageTracker.track()). Если нет активного event loop или менеджер
    ещё не поднят — событие drop'нется без шума (telemetry-style).
    """
    try:
        mgr = get_signal_manager()
        signal = Signal(type=type, payload=payload, tenant_id=tenant_id)
        mgr.publish_nowait(signal)
    except Exception:
        # Никогда не падаем — это телеметрия, основной flow важнее
        logger.debug("publish_signal failed (non-fatal)", exc_info=True)


class SignalsWebSocketHandler(WebsocketListener):
    """WS handler для real-time signals.

    Протокол:
      1. Клиент подключается на /ws/signals?user_id=...&tenant_id=...
      2. Сервер отвечает {"type":"connected","payload":{"connection_id":"..."}}
      3. (опц.) Клиент шлёт {"action":"subscribe","types":["usage_tracked",...]}
      4. Сервер пушит {"type":"...","payload":{...},"ts":"...","tenant_id":"..."}
      5. Каждые 30s — keepalive {"type":"pulse","payload":{}}
    """

    path = "/ws/signals"

    def __init__(self, owner=None) -> None:
        # Litestar Controller вызывает __init__(owner=self) при регистрации
        # роута — без `owner` параметра granian валится TypeError'ом.
        super().__init__(owner=owner)
        self.connection_id: Optional[str] = None
        self.keepalive_task: Optional[asyncio.Task] = None

    async def on_accept(
        self, socket: WebSocket,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None:
        # Идентичность — ТОЛЬКО из проверенного токена (?token= — браузер не
        # умеет ставить заголовки на WS; заголовок тоже принимаем). Раньше
        # user_id/tenant_id брались из query как есть — любой мог открыть
        # /ws/signals?tenant_id=<жертва> и получать её live-события.
        verified_uid: Optional[str] = None
        secret_configured = False
        try:
            from backend.core.auth.service_token import (
                _jwt_secret_configured, verify_service_token, verify_user_token,
            )
            secret_configured = _jwt_secret_configured()
            raw = token
            if not raw:
                auth = (socket.headers.get("authorization")
                        or socket.headers.get("Authorization") or "")
                if auth.startswith("Bearer "):
                    raw = auth.split(" ", 1)[1]
            if raw:
                claims = verify_service_token(raw) or verify_user_token(raw)
                if claims:
                    verified_uid = str(claims.get("sub")
                                       or claims.get("user_id") or "") or None
        except Exception:
            logger.debug("signals ws: token verify failed", exc_info=True)
        if verified_uid:
            user_id = verified_uid
            tenant_id = verified_uid  # tenant == user uuid во всех publisher'ах
        elif secret_configured:
            # Секрет настроен, а личность не подтверждена → тенантных событий
            # не даём: соединение живёт (pulse/connected), но в tenant-фан-аут
            # не попадает. Query-параметры без токена — не личность.
            if user_id or tenant_id:
                logger.info(
                    "signals ws: query user/tenant без валидного токена — "
                    "подписка на тенант отклонена")
            user_id = None
            tenant_id = None
        # else: dev-окружение без единого JWT-секрета — проверять нечем,
        # оставляем query-параметры (прежнее поведение только для dev).
        self.connection_id = str(uuid.uuid4())
        mgr = get_signal_manager()
        await mgr.connect(
            socket, self.connection_id,
            user_id=user_id, tenant_id=tenant_id,
        )
        # Клиент может закрыть сокет раньше приветствия (React strict-mode
        # монтирует/размонтирует дважды) — не роняем хендлер трейсбеком
        # «Transport not initialized or closed», просто отпускаем соединение.
        try:
            await socket.send_text(Signal(
                type="connected",
                payload={"connection_id": self.connection_id},
                tenant_id=tenant_id,
            ).to_json())
        except Exception as e:
            logger.info(f"Signals WS: клиент закрылся до приветствия ({e})")
            get_signal_manager().disconnect(self.connection_id)
            return
        # Keepalive: каждые 30s шлём pulse, фронт видит «живое» соединение
        async def _keepalive():
            while True:
                await asyncio.sleep(30)
                try:
                    await socket.send_text(Signal(
                        type="pulse", payload={}, tenant_id=tenant_id,
                    ).to_json())
                except Exception:
                    return
        self.keepalive_task = asyncio.create_task(_keepalive())

    async def on_disconnect(self, socket: WebSocket) -> None:
        if self.keepalive_task and not self.keepalive_task.done():
            self.keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.keepalive_task
        if self.connection_id:
            get_signal_manager().disconnect(self.connection_id)

    async def on_receive(self, socket: WebSocket, data: str) -> None:
        """Простой control-канал: subscribe/unsubscribe."""
        if not self.connection_id:
            return
        try:
            msg = json.loads(data)
            action = msg.get("action")
            if action == "subscribe":
                types = msg.get("types")
                set_types = set(types) if isinstance(types, list) else None
                get_signal_manager().set_subscription(self.connection_id, set_types)
            elif action == "ping":
                await socket.send_text(Signal(
                    type="pong", payload={},
                ).to_json())
        except Exception:
            logger.debug("Signals WS recv parse error", exc_info=True)
